# Deployment from the workstation. Server code is synchronised to
# $(SERVER_ROOT)/server and client scripts to ~/$(CLIENT_ROOT) on the Raspberry Pi.
# Generated state (payloads, certificate, rendered OpenLiteSpeed configurations, logs,
# results, venv) lives outside the synchronised trees or is excluded, so a deploy never
# overwrites data. `make fetch` brings back the Pi's results and the server's logs
# (cpu.csv, tcp_cc.csv, OpenLiteSpeed access and error logs).

SERVER      ?= ubuntu@47.129.174.173
CLIENT      ?= trungnt@100.72.249.11
SERVER_ROOT ?= /data/starlink-cc
CLIENT_ROOT ?= starlink-cc
RSYNC       := rsync -az --delete --exclude __pycache__ --exclude .DS_Store

.PHONY: deploy-server setup-server deploy-client setup-client fetch

deploy-server:
	ssh $(SERVER) 'sudo install -d -o "$$(id -un)" -g "$$(id -gn)" $(SERVER_ROOT)'
	$(RSYNC) server/ $(SERVER):$(SERVER_ROOT)/server/

setup-server: deploy-server
	ssh $(SERVER) '$(SERVER_ROOT)/server/setup.sh'

deploy-client:
	$(RSYNC) --exclude .venv --exclude results scripts/ $(CLIENT):$(CLIENT_ROOT)/

setup-client: deploy-client
	ssh $(CLIENT) '$(CLIENT_ROOT)/setup.sh'

fetch:
	mkdir -p results/client results/server
	rsync -az $(CLIENT):$(CLIENT_ROOT)/results/ results/client/
	rsync -az $(SERVER):$(SERVER_ROOT)/logs/ results/server/
