# SWAG Reverse Proxy

These examples follow the LinuxServer SWAG layout.

## Prerequisites

1. Put SWAG and Reclaimerr on the same Docker network.
2. Use `reclaimerr` as the upstream container name, or update the config.
3. Ensure your SWAG default site includes the proxy-confs include.
4. Restart SWAG after changing the proxy config.

## Example

Path:

- `/config/nginx/proxy-confs/reclaimerr.subdomain.conf`

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;

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

- If SWAG proxies via container DNS, you do not need to publish the
  app port to the host.
- Ensure the proxy forwards `X-Forwarded-For` and `X-Forwarded-Proto`.
- Use `Application URL` in General Settings for the public base URL used by
  OIDC and Plex callbacks. Keep `redirect_uri_override` for OIDC-only cases.

