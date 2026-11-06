FROM quay.io/keycloak/keycloak:26.3.2 AS builder

ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_METRICS_ENABLED=true

RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.3.2

COPY --from=builder /opt/keycloak/ /opt/keycloak/
COPY --chown=1000:0 themes/ /opt/keycloak/themes/
COPY --chmod=0555 keycloak-entrypoint.sh /opt/keycloak/bin/aelev-entrypoint.sh

USER 1000:0
ENTRYPOINT ["/opt/keycloak/bin/aelev-entrypoint.sh"]
