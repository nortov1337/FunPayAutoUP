#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="funpay-autoup"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
VENV="${APP_DIR}/.venv"
GOLDEN_KEY="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите от root: sudo bash install.sh \"<golden_key>\""
  exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 не найден. Установите его: apt install -y python3 python3-venv"
  exit 1
fi

if [[ -z "${GOLDEN_KEY}" ]]; then
  read -rsp "Вставьте golden_key и нажмите Enter: " GOLDEN_KEY
  echo
fi
if [[ -z "${GOLDEN_KEY}" ]]; then
  echo "golden_key не задан."
  exit 1
fi

echo "Каталог: ${APP_DIR}"
echo "Создаю виртуальное окружение..."
"${PYTHON_BIN}" -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip >/dev/null
"${VENV}/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "Прописываю golden_key в config.ini..."
"${VENV}/bin/python" - "${APP_DIR}/config.ini" "${GOLDEN_KEY}" <<'PY'
import configparser, sys
path, key = sys.argv[1], sys.argv[2]
parser = configparser.ConfigParser()
parser.read(path, encoding="utf-8")
if not parser.has_section("account"):
    parser.add_section("account")
parser.set("account", "golden_key", key)
with open(path, "w", encoding="utf-8") as f:
    parser.write(f)
PY

echo "Создаю systemd-сервис..."
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=FunPayAutoUP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${VENV}/bin/python ${APP_DIR}/funpay_autoup.py --config ${APP_DIR}/config.ini
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo "Готово. Статус:"
systemctl --no-pager status "${SERVICE_NAME}" || true
echo
echo "Лог сервиса: journalctl -u ${SERVICE_NAME} -f"
echo "Файл лога:   ${APP_DIR}/logs/funpay_autoup.log"
