#!/bin/bash
# Пересборка баз astra-exec(:slim):0.2 на сервере sorokin. Лог /tmp/slimD.log, маркер D_DONE.
set -e
cd /tmp/astraexec
cp /home/sorokin/repos/secure_ai_agent_platform/lfx_builder/exec/Dockerfile Dockerfile.full
cp /home/sorokin/repos/secure_ai_agent_platform/lfx_builder/exec/Dockerfile.slim Dockerfile.slim
docker build -f Dockerfile.slim -t pre-flat-slim5 . > /tmp/slimD.log 2>&1
bash /home/sorokin/repos/secure_ai_agent_platform/flatten.sh pre-flat-slim5 astra-exec-slim:0.2 >> /tmp/slimD.log 2>&1
docker build -f Dockerfile.full -t pre-flat-exec5 . >> /tmp/slimD.log 2>&1
bash /home/sorokin/repos/secure_ai_agent_platform/flatten.sh pre-flat-exec5 astra-exec:0.2 >> /tmp/slimD.log 2>&1
echo D_DONE >> /tmp/slimD.log
