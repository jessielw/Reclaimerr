# SWAG Reverse Proxy Examples

These examples follow the LinuxServer SWAG proxy config style from:

- https://github.com/linuxserver/reverse-proxy-confs

Reclaimerr defaults to port `8000` in Docker.

## Prerequisites

1. Put `swag` and `reclaimerr` on the same Docker network.
2. Use `reclaimerr` as the upstream app name (or change `$upstream_app` to your container name).
3. Ensure your SWAG default site config includes the method you use:
   - `include /config/nginx/proxy-confs/*.subdomain.conf;`

4. Restart SWAG after adding or changing proxy config files.

## Subdomain Example (Recommended)

Path:

- `/config/nginx/proxy-confs/reclaimerr.subdomain.conf`

```nginx
## Version 2026/05/30
# make sure that your reclaimerr container is named reclaimerr
# make sure that your dns has a cname set for reclaimerr
server {
    listen 443 ssl;
    # listen 443 quic;
    listen [::]:443 ssl;
    # listen [::]:443 quic;

    server_name reclaimerr.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 0;

    location / {
        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        set $upstream_app reclaimerr;
        set $upstream_port 8000;
        set $upstream_proto http;
        proxy_pass $upstream_proto://$upstream_app:$upstream_port;
    }
}
```

## Notes

- If SWAG is proxying via container DNS, you usually do not need to publish Reclaimerr's port to the host.
- If your container is not named `reclaimerr`, set `$upstream_app` accordingly.
