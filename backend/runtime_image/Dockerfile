ARG RUNTIME_BASE
FROM ${RUNTIME_BASE}
ARG DEBIAN_SNAPSHOT
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s/ bookworm main\n' "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends libstdc++6 ripgrep git bash && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --uid 10001 --create-home --home-dir /home/opencode opencode && \
    mkdir -p /workspace && chown 10001:10001 /workspace
COPY opencode /usr/local/bin/opencode
ARG OPENCODE_VERSION
RUN chmod 755 /usr/local/bin/opencode && test "$(opencode --version)" = "$OPENCODE_VERSION"
ENV HOME=/home/opencode BUN_RUNTIME_TRANSPILER_CACHE_PATH=0
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["opencode"]
