# Experiment only: do not replace the service's T5 runtime or base stack.
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS builder
RUN apt-get update \
    && apt-get install --yes --no-install-recommends cmake g++ make ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:6b58785f0a82898f4c3442417ff962e2d4b231b1bee5d033902ad90b27901e14 https://codeload.github.com/ggml-org/llama.cpp/tar.gz/d81235049384534c167caea52b85a694f6103d14 /source.tar.gz
RUN mkdir /source \
    && tar -xzf /source.tar.gz --strip-components=1 -C /source \
    && cmake -S /source -B /source/build \
       -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
       -DLLAMA_BUILD_IS_DEV=OFF -DLLAMA_BUILD_NUMBER=0 \
       -DLLAMA_BUILD_COMMIT=d81235049384534c167caea52b85a694f6103d14 \
       -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
       -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_APP=OFF \
       -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
    && cmake --build /source/build --parallel 2 --target llama-server

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
LABEL org.aelev.translator.experiment="index-translate-20261011"
LABEL org.opencontainers.image.revision="d81235049384534c167caea52b85a694f6103d14"
COPY --from=builder /source/build/bin/ /opt/llama/
ENV LD_LIBRARY_PATH=/opt/llama PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python"]
