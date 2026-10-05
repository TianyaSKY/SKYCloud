ARG MINIO_BUILDER
ARG MINIO_BASE
FROM ${MINIO_BUILDER} AS builder
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
COPY source/ ./
RUN go mod download && go mod verify
ARG MINIO_VERSION
ARG MINIO_COMMIT
ARG MINIO_RELEASE_TAG
ARG MINIO_COPYRIGHT_YEAR
RUN go build -mod=readonly -trimpath -buildvcs=false \
    -ldflags="-s -w -buildid= -X github.com/minio/minio/cmd.Version=${MINIO_VERSION} -X github.com/minio/minio/cmd.CommitID=${MINIO_COMMIT} -X github.com/minio/minio/cmd.ShortCommitID=$(printf %.12s "$MINIO_COMMIT") -X github.com/minio/minio/cmd.ReleaseTag=${MINIO_RELEASE_TAG} -X github.com/minio/minio/cmd.CopyrightYear=${MINIO_COPYRIGHT_YEAR}" \
    -o /out/minio .

FROM ${MINIO_BASE}
COPY --from=builder /out/minio /usr/local/bin/minio
COPY source/LICENSE source.tar.gz lock.json /usr/share/minio/
RUN mkdir -p /data && minio --version
EXPOSE 9000 9001
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/minio/health/cluster', timeout=2)"
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
