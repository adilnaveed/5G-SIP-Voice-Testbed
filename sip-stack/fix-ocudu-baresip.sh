#!/bin/bash
# Reapply required baresip fixes after container recreation (docker compose up / rm -f).
# These are lost every time baresip-ocudu1/2 are recreated - must be rerun after each `docker compose up`.
#
# Fixes:
#   1. net_interface -> oaitun_ue1 (use the UE's cellular tunnel, not the isolated broker eth0)
#   2. Disable ALSA module (no real sound card in container; racing/crashing kills calls)
#   3. Persisted accounts file with short regint (keeps UPF's NAT/conntrack mapping fresh so
#      server-initiated INVITEs route correctly - fixes one-directional call failures)

for c in baresip-ocudu1 baresip-ocudu2; do
  docker exec "$c" sed -i 's/^#net_interface\s*eth0/net_interface            oaitun_ue1/' /root/.baresip/config
  docker exec "$c" sed -i 's/^module                  alsa.so/#module                  alsa.so/' /root/.baresip/config
done

docker exec baresip-ocudu1 sh -c 'echo "<sip:100@192.168.70.160>;regint=15" > /root/.baresip/accounts'
docker exec baresip-ocudu2 sh -c 'echo "<sip:200@192.168.70.160>;regint=15;answermode=auto" > /root/.baresip/accounts'

docker restart baresip-ocudu1 baresip-ocudu2

echo "Fixes applied. Both UAs should auto-register on restart within a few seconds."
echo "Check with: docker attach baresip-ocudu1  then  /reginfo"
