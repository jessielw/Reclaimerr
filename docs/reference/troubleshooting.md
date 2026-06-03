# Troubleshooting

## The UI Does Not Load

- Confirm the backend is running.
- Confirm the frontend dev server is running if you are in source mode.
- Check that the configured API port is reachable.

## Scheduled Tasks Do Not Run

- Verify the task is enabled in Tasks.
- Verify the task is not waiting on a main media server.
- Check the task status and recent run history in the UI.

## Reverse Proxy Problems

- Make sure `X-Forwarded-Proto` reaches the app.
- Set `PROXY_TRUSTED_HOSTS` to the proxy IP or CIDR.
- Recheck `Application URL` in General Settings. Use
  `redirect_uri_override` only when OIDC needs a different callback.

## Deletion Is Skipped

- Protected media is skipped by design.
- Pending protection requests block automatic deletion.
- Pending delete requests also block automatic deletion.

