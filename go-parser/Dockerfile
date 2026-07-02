FROM golang:1.23-alpine AS builder

WORKDIR /build
COPY . .

RUN go env -w GOPROXY=https://goproxy.cn,direct && \
    go mod download && \
    go build -o ivd_parser .

FROM alpine:latest
RUN apk --no-cache add ca-certificates
WORKDIR /app
COPY --from=builder /build/ivd_parser .
EXPOSE 8082
CMD ["./ivd_parser"]
