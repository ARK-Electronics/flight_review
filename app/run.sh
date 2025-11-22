#!/bin/bash

PORT_VALUE=${PORT:-5006}
DOMAIN_VALUE=${DOMAIN:-*}

WORK_PATH=/opt/service
DATA_PATH=${STORAGE_PATH:-/opt/data}

# app setup
echo "Starting run.sh..."
echo "STORAGE_PATH is set to: '${STORAGE_PATH}'"
echo "DATA_PATH is set to: '${DATA_PATH}'"

if [ ! -d ${DATA_PATH} ]; then
	echo "Creating DATA_PATH: ${DATA_PATH}"
	mkdir -p ${DATA_PATH}
fi

echo "Listing contents of ${DATA_PATH}:"
ls -la ${DATA_PATH}

if [ ! -f ${DATA_PATH}/logs.sqlite ]; then
	echo "Database not found at ${DATA_PATH}/logs.sqlite. Initializing..."
	python3 ${WORK_PATH}/setup_db.py
else
	echo "Database found at ${DATA_PATH}/logs.sqlite"
fi


if [ -n "${USE_PROXY}" ]; then
	echo "Use Proxy!"
	python3 ${WORK_PATH}/serve.py \
		--port=${PORT_VALUE} \
		--address=0.0.0.0 \
		--allow-websocket-origin=${DOMAIN_VALUE} \
		--use-xheaders
else
	python3 ${WORK_PATH}/serve.py --port=${PORT_VALUE}
fi
