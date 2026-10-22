# Vendored API docs assets

Served at `/docs-assets` for the `/docs`, `/redoc`, `/api/v1/docs` and
`/api/v1/redoc` pages, so they don't load script from a third-party CDN.

| Package           | Version | Files                                       |
| ----------------- | ------- | ------------------------------------------- |
| `swagger-ui-dist` | 5.34.0  | `swagger-ui-bundle.js`, `swagger-ui.css`    |
| `redoc`           | 2.5.4   | `bundles/redoc.standalone.js`               |

`favicon.png` is a copy of `frontend/static/favicon-32x32.png`.

## Updating

Fetch from the npm registry (`npm pack` verifies the package integrity hash),
not from a CDN:

```sh
npm pack swagger-ui-dist@<version> redoc@<version>
```

Copy the files listed above, plus each package's `LICENSE` (and Swagger UI's
`NOTICE`) and the matching `*.LICENSE.txt`, then update the versions here.
