FROM scratch

LABEL maintainer="H100 Platform Automation <codexops@localhost>"
LABEL org.opencontainers.image.source="https://github.com/grafana/grafana"
LABEL org.opencontainers.image.version="13.1.1"
LABEL org.opencontainers.image.revision="e784b7782a43822971f896b3377dc736346a9c66"
LABEL org.opencontainers.image.description="Grafana OSS 13.1.1 from the checksum-verified official static release archive"
LABEL h100.grafana.archive.sha256="0c07116968aea49768af8babd3c3f162d19012655a1a220cd7a9d97efe91da6c"

ENV PATH="/usr/share/grafana/bin" \
    HOME="/usr/share/grafana" \
    GF_PATHS_CONFIG="/etc/grafana/grafana.ini" \
    GF_PATHS_DATA="/var/lib/grafana" \
    GF_PATHS_HOME="/usr/share/grafana" \
    GF_PATHS_LOGS="/var/lib/grafana/log" \
    GF_PATHS_PLUGINS="/var/lib/grafana/plugins" \
    GF_PATHS_PROVISIONING="/etc/grafana/provisioning"

WORKDIR /usr/share/grafana

COPY --chown=472:0 .h100-image/passwd /etc/passwd
COPY --chown=472:0 .h100-image/group /etc/group
COPY --chown=0:0 .h100-image/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --chown=472:0 bin/ /usr/share/grafana/bin/
COPY --chown=472:0 public/ /usr/share/grafana/public/
COPY --chown=472:0 conf/ /usr/share/grafana/conf/
COPY --chown=472:0 plugins-bundled/ /usr/share/grafana/plugins-bundled/
COPY --chown=472:0 LICENSE /usr/share/grafana/LICENSE
COPY --chown=472:0 NOTICE.md /usr/share/grafana/NOTICE.md
COPY --chown=472:0 VERSION /usr/share/grafana/VERSION
COPY --chown=472:0 conf/sample.ini /etc/grafana/grafana.ini

EXPOSE 3000
USER 472:0
STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/share/grafana/bin/grafana", "server", "--homepath=/usr/share/grafana", "--config=/etc/grafana/grafana.ini", "--packaging=docker"]
CMD ["cfg:default.log.mode=console"]
