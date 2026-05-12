# At first add firewall rule to log https connections coming from your scale
nano /jffs/scripts/firewall-start

#!/bin/sh

SCALE_IP="your.scale.ip"

iptables -t nat -I PREROUTING -s $SCALE_IP -p tcp --dport 443 \
  -m state --state NEW \
  -j LOG --log-prefix "WITHINGS: " --log-level 4

chmod +x /jffs/scripts/firewall-start

# Then create a script that polls syslog for these messages and if found, sends notification
nano /jffs/scripts/withings_watch.sh

#!/bin/sh
HA_URL="http://your.withings2mqtt.address:8888/api/webhook/withings_sync"
COOLDOWN=60
LAST_TRIGGER=0

tail -f /tmp/syslog.log | awk '/WITHINGS:/ { print; fflush() }' | while read line; do
    NOW=$(date +%s)
    if [ $((NOW - LAST_TRIGGER)) -gt $COOLDOWN ]; then
        LAST_TRIGGER=$NOW
        logger -t withings "Scale sync detected, notifying HA..."
        sleep 15
        curl -s -X POST "$HA_URL" &
    fi
done

chmod +x /jffs/scripts/withings_watch.sh

# Then we add it to started at reboot
nano /jffs/scripts/services-start

#!/bin/sh
# Start the watcher
/jffs/scripts/withings_watch.sh &

chmod +x /jffs/scripts/services-start

