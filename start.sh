#!/bin/bash
# Startet das virtuelle Finanzdashboard mit echten Marktdaten.
cd "$(dirname "$0")" || exit 1
exec python3 server.py "$@"
