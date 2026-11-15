FROM rust:1.91-bookworm AS build
WORKDIR /app/backend-rust
COPY backend-rust/Cargo.toml backend-rust/Cargo.lock* ./
COPY backend-rust/src ./src
COPY backend-rust/tests ./tests
COPY backend-rust/migrations ./migrations
RUN cargo build --release --bins --locked

FROM build AS test
RUN rustup component add rustfmt clippy \
    && cargo fmt --all --check \
    && cargo clippy --locked --all-targets --all-features -- -D warnings \
    && cargo test --locked --all-targets --all-features

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --create-home hedgehoog
COPY --from=build /app/backend-rust/target/release/hedgehoog-api /usr/local/bin/hedgehoog-api
COPY --from=build /app/backend-rust/target/release/hedgehoog-migrate /usr/local/bin/hedgehoog-migrate
COPY --from=build /app/backend-rust/target/release/hedgehoog-worker /usr/local/bin/hedgehoog-worker
COPY --from=build /app/backend-rust/target/release/hedgehoog-scheduler /usr/local/bin/hedgehoog-scheduler
USER hedgehoog
EXPOSE 8000
CMD ["hedgehoog-api"]
