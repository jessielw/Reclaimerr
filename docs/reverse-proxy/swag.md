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
#    listen 443 quic;
    listen [::]:443 ssl;
#    listen [::]:443 quic;

    server_name reclaimerr.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 0;

    # enable for ldap auth (requires ldap-location.conf in the location block)
    #include /config/nginx/ldap-server.conf;

    # enable for Authelia (requires authelia-location.conf in the location block)
    #include /config/nginx/authelia-server.conf;

    # enable for Authentik (requires authentik-location.conf in the location block)
    #include /config/nginx/authentik-server.conf;

    # enable for Tinyauth (requires tinyauth-location.conf in the location block)
    #include /config/nginx/tinyauth-server.conf;

    location / {
        # enable the next two lines for http auth
        #auth_basic "Restricted";
        #auth_basic_user_file /config/nginx/.htpasswd;

        # enable for ldap auth (requires ldap-server.conf in the server block)
        #include /config/nginx/ldap-location.conf;

        # enable for Authelia (requires authelia-server.conf in the server block)
        #include /config/nginx/authelia-location.conf;

        # enable for Authentik (requires authentik-server.conf in the server block)
        #include /config/nginx/authentik-location.conf;

        # enable for Tinyauth (requires tinyauth-server.conf in the server block)
        #include /config/nginx/tinyauth-location.conf;

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
- Ensure your proxy forwards `X-Forwarded-For` and `X-Forwarded-Proto` headers (SWAG `proxy.conf` does this).
- For correct HTTPS callback URLs (OIDC and Plex browser sign-in), set `PROXY_TRUSTED_HOSTS` in Reclaimerr to your SWAG proxy IP/CIDR. Use `*` only if your Reclaimerr API is not directly exposed.
- If an OIDC provider needs a hard-coded callback URL, you can still set `redirect_uri_override` in Authentication settings.
