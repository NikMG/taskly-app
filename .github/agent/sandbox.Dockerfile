# Runtime images only: no project code and no dependencies are baked in.
# Dependencies are installed by prepare_sandbox.sh and mounted read-only.

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS backend
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
USER 10001

FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS frontend
RUN adduser -D -H -u 10001 -s /sbin/nologin sandbox
USER 10001
