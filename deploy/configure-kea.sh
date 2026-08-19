#!/bin/sh
set -eu

live_config=/etc/kea/kea-dhcp4.conf
managed_config=/etc/kea/sonic-ztp/kea-dhcp4.conf
backup_config=/etc/kea/kea-dhcp4.conf.pre-sonic-ztp

if [ ! -e "$backup_config" ]; then
    cp --dereference "$live_config" "$backup_config"
fi
cp --dereference "$live_config" "$managed_config"
chown ahbee:_kea "$managed_config"
chmod 0640 "$managed_config"
ln -sfn "$managed_config" "$live_config"
install -m 0644 /home/ahbee/sonic-ztp-server/deploy/sonic-ztp-server.service /etc/systemd/system/sonic-ztp-server.service
systemctl daemon-reload
