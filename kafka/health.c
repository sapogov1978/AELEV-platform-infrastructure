/* Bounded read-only Kafka probe: ApiVersions v0 and empty Metadata v1.
 * No JVM, DNS, child processes, credentials, topics or application messages.
 * Protocol: https://kafka.apache.org/39/design/protocol/
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define CAPACITY 65536
#define DEADLINE_MS 4000

typedef struct {
    const unsigned char *data;
    size_t size, offset;
    int valid;
} Reader;

static int64_t now_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) return -1;
    return (int64_t)value.tv_sec * 1000 + value.tv_nsec / 1000000;
}

static int wait_socket(int fd, short events, int64_t deadline) {
    for (;;) {
        int64_t now = now_ms(), remaining = deadline - now;
        if (now < 0 || remaining <= 0) return 0;
        struct pollfd item = {.fd = fd, .events = events};
        int result = poll(&item, 1, (int)remaining);
        if (result < 0 && errno == EINTR) continue;
        return result > 0 && !(item.revents & (POLLERR | POLLNVAL))
               && (item.revents & (events | POLLHUP));
    }
}

static int transfer(int fd, unsigned char *buffer, size_t count,
                    int writing, int64_t deadline) {
    size_t done = 0;
    while (done < count) {
        if (!wait_socket(fd, writing ? POLLOUT : POLLIN, deadline)) return 0;
        ssize_t size = writing ? send(fd, buffer + done, count - done, 0)
                               : recv(fd, buffer + done, count - done, 0);
        if (size < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
            continue;
        if (size <= 0) return 0;
        done += (size_t)size;
    }
    return 1;
}

static uint32_t take(Reader *r, size_t size) {
    if (!r->valid || r->offset > r->size || size > r->size - r->offset) {
        r->valid = 0;
        return 0;
    }
    uint32_t result = 0;
    for (size_t i = 0; i < size; ++i) result = (result << 8) | r->data[r->offset++];
    return result;
}

static int string_field(Reader *r, int nullable) {
    uint32_t size = take(r, 2);
    if (size == 65535 && nullable) return r->valid;
    if (!r->valid || (!nullable && size == 0) || size > 32767
        || size > r->size - r->offset) return 0;
    r->offset += size;
    return 1;
}

static void put(unsigned char *p, uint32_t value, size_t size) {
    for (size_t i = 0; i < size; ++i) {
        p[size - i - 1] = (unsigned char)(value & 255);
        value >>= 8;
    }
}

static int exchange(int fd, unsigned key, unsigned version, unsigned correlation,
                    unsigned char *answer, Reader *reader, int64_t deadline) {
    const char client[] = "aelev-kafka-health";
    unsigned char request[128] = {0};
    size_t header_size = 10 + sizeof(client) - 1;
    size_t body_size = header_size + (key == 3 ? 4 : 0);
    put(request, (uint32_t)body_size, 4);
    put(request + 4, key, 2);
    put(request + 6, version, 2);
    put(request + 8, correlation, 4);
    put(request + 12, sizeof(client) - 1, 2);
    memcpy(request + 14, client, sizeof(client) - 1);
    /* Zero topics, not a null array: never enumerate or auto-create topics. */
    if (!transfer(fd, request, body_size + 4, 1, deadline)) return 0;
    unsigned char length[4];
    if (!transfer(fd, length, 4, 0, deadline)) return 0;
    Reader prefix = {.data = length, .size = 4, .valid = 1};
    uint32_t size = take(&prefix, 4);
    if (size < 4 || size > CAPACITY) return 0;
    if (!transfer(fd, answer, size, 0, deadline)) return 0;
    *reader = (Reader){.data = answer, .size = size, .valid = 1};
    return take(reader, 4) == correlation && reader->valid;
}

static int versions_valid(Reader *r) {
    if (take(r, 2) != 0 || !r->valid) return 0;
    uint32_t count = take(r, 4);
    if (!r->valid || count == 0 || count > 2048 || r->size - r->offset != count * 6)
        return 0;
    unsigned char seen[4096] = {0};
    int metadata = 0, versions = 0;
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t key = take(r, 2), low = take(r, 2), high = take(r, 2);
        if (!r->valid || key > 32767 || high > 32767 || low > high
            || (seen[key / 8] & (1u << (key % 8)))) return 0;
        seen[key / 8] |= (unsigned char)(1u << (key % 8));
        if (key == 3 && low <= 1 && high >= 1) metadata = 1;
        if (key == 18 && low == 0) versions = 1;
    }
    return r->valid && r->offset == r->size && metadata && versions;
}

static int metadata_valid(Reader *r) {
    uint32_t count = take(r, 4), brokers[1024];
    if (!r->valid || count == 0 || count > 1024) return 0;
    for (uint32_t i = 0; i < count; ++i) {
        brokers[i] = take(r, 4);
        if (!r->valid || brokers[i] > INT32_MAX || !string_field(r, 0)) return 0;
        uint32_t port = take(r, 4);
        if (port == 0 || port > 65535 || !string_field(r, 1)) return 0;
        for (uint32_t j = 0; j < i; ++j) if (brokers[i] == brokers[j]) return 0;
    }
    uint32_t controller = take(r, 4), topics = take(r, 4);
    int controller_found = 0;
    for (uint32_t i = 0; i < count; ++i)
        if (brokers[i] == controller) controller_found = 1;
    return r->valid && r->offset == r->size && topics == 0 && controller_found;
}

static int failed(const char *code, int64_t deadline) {
    fprintf(stderr, "KAFKA_HEALTH_%s\n", now_ms() >= deadline ? "DEADLINE" : code);
    return 1;
}

int main(int argc, char **argv) {
    unsigned long port = 9092;
    if (argc == 2) {
        char *end = NULL;
        if (!*argv[1] || strspn(argv[1], "0123456789") != strlen(argv[1])) return 2;
        errno = 0;
        port = strtoul(argv[1], &end, 10);
        if (errno || !end || *end || port == 0 || port > 65535) return 2;
    } else if (argc != 1) return 2;
    int64_t start = now_ms();
    if (start < 0) return 1;
    int64_t deadline = start + DEADLINE_MS;
    signal(SIGPIPE, SIG_IGN);
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return failed("SOCKET", deadline);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1) {
        close(fd);
        return failed("SOCKET", deadline);
    }
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        close(fd);
        return failed("SOCKET", deadline);
    }
    int connected = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (connected < 0) {
        int error = 0;
        socklen_t length = sizeof(error);
        if (errno != EINPROGRESS || !wait_socket(fd, POLLOUT, deadline)
            || getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error) {
            close(fd);
            return failed("CONNECT", deadline);
        }
    }
    unsigned char answer[CAPACITY];
    Reader response;
    const char *error = NULL;
    if (!exchange(fd, 18, 0, 1, answer, &response, deadline)) error = "FRAME";
    else if (!versions_valid(&response)) error = "API_VERSIONS";
    else if (!exchange(fd, 3, 1, 2, answer, &response, deadline)) error = "FRAME";
    else if (!metadata_valid(&response)) error = "METADATA";
    close(fd);
    return error ? failed(error, deadline) : 0;
}
