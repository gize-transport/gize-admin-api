# GZ-086: shared by dump.sh, verify.sh and restore.sh (sourced, never run). Postgres 17's
# tools run in the image docker-compose and CI pin; connection URLs reach them through the
# container's environment only, with the password apart (PGPASSWORD), so no process
# argument ever holds it. Works with macOS's bash 3.2.

POSTGRES_IMAGE="postgres:17-trixie@sha256:b8faeff7712663c2773a190181e1246e37e364b887611c04658c28b44e2408fe"

# pg_connection NAME: read the URL in the variable NAME (the app's form, ?ssl=verify-full,
# or a plain local one) and export PGURL (without its password) and PGPASSWORD for the
# tools. Off this machine it must verify the server's certificate (sslmode=verify-full),
# as the app and the bootstrap do. Messages never echo the URL.
pg_connection() {
  local name="$1" url rest host
  url="${!name:-}"
  if [ -z "${url}" ]; then
    echo "set ${name}" >&2
    return 2
  fi
  url="${url//ssl=verify-full/sslmode=verify-full}"  # asyncpg's spelling, then libpq's
  local pattern='^(postgres(ql)?://)([^:@/]+)(:([^@]*))?@(.+)$'
  if ! [[ "${url}" =~ ${pattern} ]]; then
    echo "${name} must look like postgresql://user:password@host:port/database" >&2
    return 2
  fi
  rest="${BASH_REMATCH[6]}"
  export PGURL="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${rest}"
  export PGPASSWORD="${BASH_REMATCH[5]}"
  host="${rest%%/*}"
  host="${host%:*}"
  case "${host}" in
    127.0.0.1 | localhost | "[::1]") ;;
    *)
      if ! [[ "${rest}" =~ [?\&]sslmode=verify-full(\&|$) ]]; then
        echo "${name} must set ssl=verify-full off this machine" >&2
        return 2
      fi
      ;;
  esac
}

# pg_docker_run MOUNT COMMAND...: run COMMAND in the tools' image with MOUNT (a docker -v
# value), this user's IDs, the host's network, the connection from the environment, a
# 10-second connect timeout and, when DATABASE_SSL_ROOT_CERT names one, the CA to verify
# the server against. Standard input goes to COMMAND.
pg_docker_run() {
  local mount="$1"
  shift
  local cert=()
  if [ -n "${DATABASE_SSL_ROOT_CERT:-}" ]; then
    local cert_file
    cert_file="$(cd "$(dirname "${DATABASE_SSL_ROOT_CERT}")" && pwd)/$(basename "${DATABASE_SSL_ROOT_CERT}")"
    cert=(-v "${cert_file}:/certs/root.crt:ro" -e PGSSLROOTCERT=/certs/root.crt)
  fi
  docker run --rm -i --network host --user "$(id -u):$(id -g)" \
    -e PGURL -e PGPASSWORD -e PGCONNECT_TIMEOUT=10 ${cert[@]+"${cert[@]}"} \
    -v "${mount}" "${POSTGRES_IMAGE}" "$@"
}
