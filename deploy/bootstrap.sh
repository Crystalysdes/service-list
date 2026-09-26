#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu / Debian server for the Service List bot.
#
# Run as root in an interactive terminal (NOT "curl | bash", it needs your keyboard):
#   bash <(curl -fsSL https://raw.githubusercontent.com/Crystalysdes/service-list/claude/amazing-pasteur-j0islp/deploy/bootstrap.sh)
#
# Installs Docker, downloads the bot to /opt/service-list, asks for the settings (bot token, owners,
# Crypto Pay, backup password), enables the firewall, starts the bot and prints the SSH deploy key for
# GitHub Actions. Safe to run again: existing settings are kept unless you change them.
set -euo pipefail

REPO="${SL_REPO:-https://github.com/Crystalysdes/service-list.git}"
BRANCH="${SL_BRANCH:-claude/amazing-pasteur-j0islp}"
DIR="${SL_DIR:-/opt/service-list}"

if [[ -t 1 ]]; then
    B=$'\033[1m' G=$'\033[32m' Y=$'\033[33m' R=$'\033[31m' C=$'\033[36m' N=$'\033[0m'
else
    B="" G="" Y="" R="" C="" N=""
fi
step() { printf '\n%s==> %s%s\n' "$C$B" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N" >&2; }
die() {
    printf '%s✗ %s%s\n' "$R" "$*" "$N" >&2
    exit 1
}

[[ "$(id -u)" == 0 ]] || die "Запустите от root (например, после «sudo -i»)."
[[ -t 0 ]] || die "Нужен обычный терминал: запустите командой bash <(curl -fsSL …), а не через «| bash»."
# shellcheck source=/dev/null
. /etc/os-release
case "${ID:-}" in
ubuntu | debian) ;;
*) die "Поддерживаются Ubuntu 22.04/24.04 и Debian 12, а здесь: ${PRETTY_NAME:-неизвестная система}." ;;
esac

step "Устанавливаю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates openssl ufw openssh-client openssh-server iproute2 >/dev/null
systemctl enable --now ssh >/dev/null 2>&1 || true

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    step "Устанавливаю Docker"
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || die "Docker Compose не установился — попробуйте запустить скрипт ещё раз."

step "Скачиваю бота в $DIR"
if [[ -d "$DIR/.git" ]]; then
    git -C "$DIR" fetch --quiet origin "$BRANCH"
    git -C "$DIR" checkout --quiet --force -B "$BRANCH" "origin/$BRANCH"
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi
printf '%s\n' "$BRANCH" >"$DIR/.deploy-branch"
install -m 755 "$DIR/deploy/servicelist" /usr/local/bin/servicelist

step "Настройки бота"
SL_DIR="$DIR" SL_NO_RESTART=1 servicelist config

# the port(s) sshd really listens on — never lock out a session on a non-standard port
ssh_ports=$(/usr/sbin/sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | sort -u)
[[ -n "$ssh_ports" ]] || ssh_ports=22
step "Файрвол: открыт только SSH (порт $(echo "$ssh_ports" | paste -sd, -)); боту входящие порты не нужны"
for port in $ssh_ports; do
    ufw allow "$port/tcp" >/dev/null
done
ufw --force enable >/dev/null
ufw status | head -n 6

step "Собираю и запускаю бота (первый раз это займёт 2–4 минуты)"
if SL_DIR="$DIR" servicelist deploy; then
    (cd "$DIR" && docker compose logs --tail 15 bot) || true
    started=1
else
    warn "Бот пока не запустился. Проверьте настройки: «servicelist config», журнал: «servicelist logs»."
    started=0
fi

step "Ключ для автоматического обновления из GitHub"
SL_DIR="$DIR" servicelist deploy-key

printf '\n%s%s════ ГОТОВО ════%s\n' "$B" "$G" "$N"
if [[ "$started" == 1 ]]; then
    printf '1. Откройте бота в Telegram и отправьте /admin — дальше мастер настройки (см. README).\n'
else
    printf '1. Сначала добейтесь запуска бота: servicelist config, затем servicelist deploy.\n'
fi
printf '2. Добавьте три секрета в GitHub (выше) — после этого обновления будут приходить сами.\n'
printf '3. Команды на сервере: servicelist status | logs | restart | config | backup | deploy\n'
