#!/bin/sh
set -eu

APP_USER="${APP_USER:-reclaimerr}"
APP_GROUP="${APP_GROUP:-reclaimerr}"
APP_HOME="${APP_HOME:-/app/data}"

fail() {
	echo "ERROR: $*" >&2
	exit 1
}

is_numeric() {
	case "$1" in
		*[!0-9]* | "") return 1 ;;
		*) return 0 ;;
	esac
}

is_valid_umask() {
	case "$1" in
		"" | *[!0-7]* ) return 1 ;;
		? | ?? | ??? | ????) return 0 ;;
		*) return 1 ;;
	esac
}

configure_timezone() {
	target_tz="$1"
	zoneinfo_path="/usr/share/zoneinfo/$target_tz"

	if [ ! -e "$zoneinfo_path" ]; then
		fail "TZ '$target_tz' is not a valid timezone in /usr/share/zoneinfo"
	fi

	ln -snf "$zoneinfo_path" /etc/localtime
	echo "$target_tz" > /etc/timezone
}

ensure_group() {
	target_gid="$1"
	group_by_gid="$(getent group "$target_gid" | cut -d: -f1 || true)"

	if [ -n "$group_by_gid" ]; then
		APP_GROUP="$group_by_gid"
		return
	fi

	if getent group "$APP_GROUP" >/dev/null 2>&1; then
		groupmod -o -g "$target_gid" "$APP_GROUP"
	else
		groupadd -o -g "$target_gid" "$APP_GROUP"
	fi
}

ensure_user() {
	target_uid="$1"
	target_gid="$2"
	user_by_uid="$(getent passwd "$target_uid" | cut -d: -f1 || true)"

	if [ -n "$user_by_uid" ]; then
		APP_USER="$user_by_uid"
		usermod -g "$target_gid" "$APP_USER"
		return
	fi

	if id "$APP_USER" >/dev/null 2>&1; then
		usermod -o -u "$target_uid" -g "$target_gid" "$APP_USER"
	else
		if [ "$target_uid" -lt 1000 ]; then
			useradd -r -o -u "$target_uid" -g "$target_gid" -d "$APP_HOME" -s /usr/sbin/nologin "$APP_USER"
		else
			useradd -o -u "$target_uid" -g "$target_gid" -d "$APP_HOME" -s /usr/sbin/nologin "$APP_USER"
		fi
	fi
}

if [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
	if [ -z "${PUID:-}" ] || [ -z "${PGID:-}" ]; then
		fail "PUID and PGID must be set together"
	fi
	if ! is_numeric "$PUID"; then
		fail "PUID must be a numeric UID"
	fi
	if ! is_numeric "$PGID"; then
		fail "PGID must be a numeric GID"
	fi
fi

if [ -n "${UMASK:-}" ] && ! is_valid_umask "$UMASK"; then
	fail "UMASK must be a 1-4 digit octal value such as 022 or 002"
fi

if [ -n "${TZ:-}" ]; then
	configure_timezone "$TZ"
fi

mkdir -p /app/data/database /app/data/logs /app/data/static/avatars

if [ -n "${PUID:-}" ] && [ -n "${PGID:-}" ]; then
	ensure_group "$PGID"
	ensure_user "$PUID" "$PGID"
	chown -R "$PUID:$PGID" /app/data
fi

if [ -n "${UMASK:-}" ]; then
	umask "$UMASK"
fi

if [ -n "${PUID:-}" ] && [ -n "${PGID:-}" ] && { [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; }; then
	exec gosu "$APP_USER:$APP_GROUP" "$@"
fi

exec "$@"
