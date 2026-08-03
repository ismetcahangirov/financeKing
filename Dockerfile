# financeKing application image.
#
# Two stages so that uv and the build toolchain do not ship in the runtime
# image. The runtime carries the interpreter, the virtualenv and the source --
# nothing that can compile a wheel, which is one fewer thing an agent-authored
# dependency can reach for at runtime.

# python:3.12-slim-bookworm -- pinned 2026-08-03. The interpreter minor version
# is pinned in pyproject.toml as >=3.12,<3.13 for the same reason it is pinned
# here: CI, Compose and the developer machine must run the same interpreter,
# because a version skew in a money-handling codebase surfaces as a behaviour
# difference nobody attributes to Python.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

# ghcr.io/astral-sh/uv:0.5.11 -- pinned 2026-08-03. Copied from the published
# image rather than installed by a curl-to-shell, which is unpinned by
# construction and executes whatever the endpoint serves on the day.
COPY --from=ghcr.io/astral-sh/uv@sha256:0ac957607303916420297a4c9c213bb33fbd3c888f9cd7f4f7273596ebf42b85 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The lockfile layer is separate from the source layer so that editing a Python
# file does not re-resolve and re-download every dependency. --frozen refuses to
# update the lockfile: a build that silently re-resolves is a build whose
# dependency set is a function of when it ran.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

# Non-root. The container holds a read-only mount of the Ed25519 private key,
# and a process that does not need to be root should not be able to change the
# mode of anything it can see. Full container hardening -- read-only rootfs,
# dropped capabilities, no default egress -- is #107; this is the part that
# belongs with the image rather than with the runtime policy.
RUN groupadd --system --gid 1001 fking \
    && useradd --system --uid 1001 --gid fking --create-home --shell /usr/sbin/nologin fking

WORKDIR /app

COPY --from=builder --chown=fking:fking /app/.venv /app/.venv
COPY --from=builder --chown=fking:fking /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Owned by root, writable by the app user: the parquet archive and any runtime
# scratch. Created here so the volume mount inherits the right ownership rather
# than arriving root-owned and unwritable on first start.
RUN mkdir -p /data/parquet && chown -R fking:fking /data

USER fking

# The boot sequence that exists today: validate configuration, resolve every
# venue endpoint against the compiled-in allowlist, log the allowlist, exit 0 --
# or exit 78 (EX_CONFIG) so a supervisor can tell a configuration error from a
# crash and decline to retry it.
#
# This is a one-shot. The long-running process does not exist yet: the runtime
# and event bus are #18, the FastAPI surface and its /health/ready endpoint are
# #102. When it does, this CMD becomes `alembic upgrade head` followed by the
# server -- the order DEPLOYMENT.md 5 specifies -- and the compose file gains
# the health check that goes with it. Migrations exist as of #17 but are run by
# `make migrate` rather than from here, because a schema change applied by a
# container start is a schema change nobody watched.
CMD ["python", "-m", "fking.platform.config"]
