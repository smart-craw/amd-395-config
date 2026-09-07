## Lightning fast qwen-next

Halogen is closed source and should be treated as "untrusted".

### Basic use (not recommended):

[Reference](https://github.com/peonist-ai/halogen-flash-server).

* Download weights from [hf](https://huggingface.co/peonist-ai/halogen-qwen3.8-flash-next) to `~/models/qwen3.8-flash-halogen`
* Run the following command:

```sh
podman run --rm -p 8731:8731 \
  --device /dev/kfd --device /dev/dri --group-add keep-groups \
  --security-opt seccomp=unconfined --ipc=host --ulimit memlock=-1:-1 \
  -v ~/models/qwen3.8-flash-halogen:/models:ro \
  ghcr.io/peonist-ai/halogen-flash-server:0.4.4
```

However, this is not yet open sourced and so should be treated as "untrusted".

### Complex use (standalone server)

Block outbound traffic at the network level.  This guide allows outbound on
* 1.1.1.1 Port 53 (DNS)
* 9.9.9.9 Port 53 (DNS)
* docker.io
* ghcr.io
* ubuntu package repositories

```sh
## create the subnet to run halogen in.  Needed to keep isolated from other containers, but doesn't isolate from internet/host.
podman network create webserver-net --subnet 10.89.10.0/24
```


```sh
## fix dns/53
sudo mkdir -p /etc/systemd/resolved.conf.d

sudo tee /etc/systemd/resolved.conf.d/disable-stub.conf <<'EOF'
[Resolve]
DNSStubListener=no
EOF

sudo systemctl restart systemd-resolved
```

```sh
## needed for dns -> ip
# nftables uses ip, so need to get IPs from hostname
sudo apt install -y dnsmasq
```

```sh
sudo tee /etc/dnsmasq.d/allowlist.conf <<'EOF'
# Only listen locally — don't become an open resolver
listen-address=127.0.0.1
bind-interfaces
port=53

# Upstream resolvers
server=1.1.1.1
server=9.9.9.9

# Don't read /etc/hosts weirdness or use DNS cache tricks that hide updates
cache-size=1000
no-resolv

# As each of these domains resolves, push the IP into the matching
# nftables set (family 4 = IPv4, table 'inet filter', set 'allowed_v4')
nftset=/docker.io/4#inet#filter#allowed_v4
nftset=/registry-1.docker.io/4#inet#filter#allowed_v4
nftset=/auth.docker.io/4#inet#filter#allowed_v4
nftset=/production.cloudflare.docker.com/4#inet#filter#allowed_v4
nftset=/ghcr.io/4#inet#filter#allowed_v4
nftset=/pkg-containers.githubusercontent.com/4#inet#filter#allowed_v4
nftset=/archive.ubuntu.com/4#inet#filter#allowed_v4
nftset=/security.ubuntu.com/4#inet#filter#allowed_v4
nftset=/changelogs.ubuntu.com/4#inet#filter#allowed_v4
EOF

```

```sh
# point at localhost for dns resolution
echo "nameserver 127.0.0.1" | sudo tee /etc/resolv.conf
```

```sh
## Set the allowlist (53 on select IPs, docker.io, ghcr.io, ubuntu repositories)
sudo tee /etc/nftables-allowlist.conf <<'EOF'
table inet filter
delete table inet filter

table inet filter {
    set allowed_v4 {
        type ipv4_addr
        flags interval,timeout
    }

    chain output {
        type filter hook output priority filter; policy drop;

        oif lo accept
        ct state established,related accept

        # DNS to dnsmasq
        ip daddr 127.0.0.1 udp dport 53 accept
        ip daddr 127.0.0.1 tcp dport 53 accept
        ip daddr { 1.1.1.1, 9.9.9.9 } udp dport 53 accept
        ip daddr { 1.1.1.1, 9.9.9.9 } tcp dport 53 accept

        # Only allow-listed registry/mirror IPs, populated live by dnsmasq
        ip daddr @allowed_v4 tcp dport { 80, 443 } accept
    }
}
EOF

sudo nft -f /etc/nftables-allowlist.conf
```

```sh
## Persist across reboots
sudo tee -a /etc/nftables.conf <<'EOF'
include "/etc/nftables-allowlist.conf"
EOF

sudo systemctl enable nftables
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
```

Then run the `halogen` image as follows:

```sh
podman run --rm --network webserver-net \
  -p 8731:8731 \
  --device /dev/kfd --device /dev/dri --group-add keep-groups \
  --security-opt seccomp=unconfined --ipc=host --ulimit memlock=-1:-1 \
  -v ~/models/qwen3.8-flash-halogen:/models:ro \
  ghcr.io/peonist-ai/halogen-flash-server:0.4.4
```

### Full docker compose

While not tested, it should be possible to provide network segmentation within a docker compose application.  Something like squid could be used for outbound.  There could be a reverse proxy inbound.  The halogen container would only have network access to the reverse proxy pod and the squid pod.  This would allow the host (server) to have full access to the internet, if desired.
