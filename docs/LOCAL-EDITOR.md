# Local editor sessions

The editor listens on `127.0.0.1`. The launcher supplies `HOMESCREEN_UI_TOKEN`
and opens the editor with a `#token=...` fragment. The browser consumes and
removes that fragment, then establishes a session with the endpoint below.
API credentials are transmitted in request headers.

## Authenticate [since this fork]

`POST /v1/authenticate`

Send the launch token in the `X-Token` header. A successful response includes
`ok` and a `credential` for that editor origin. The browser keeps the credential
in the tab's session storage across reloads and sends it in `X-Editor-Session`
on same-origin API and icon requests. Browser mutation requests include the
matching local `Origin` header. Direct clients can use `X-Token` on API requests.
The existing `/api/auth` route remains an alias.

## Backend health [since this fork]

`GET /v1/health`

The native launcher sets `HOMESCREEN_HEALTH_SECRET` separately from the UI token.
It sends a fresh 64-character hexadecimal `X-Homescreen-Challenge` header.
The response contains:

```json
{"app": "homescreen", "ready": true, "proof": "<64-character hexadecimal HMAC>"}
```

`proof` is HMAC-SHA256 over the challenge's UTF-8 bytes, using the health secret's
UTF-8 bytes as the key. The launcher validates this response before opening the
editor. The default standalone CLI configuration uses `X-Token` authentication
and returns `app` and `ready`. Setting `HOMESCREEN_HEALTH_SECRET` selects the
challenge-response flow described above.
The existing `/health` route remains an alias.

## Authentication and session errors

Authentication/session responses use the following error representation:

```json
{
  "error": {
    "message": "authentication required",
    "type": "authentication_error",
    "param": null,
    "code": "invalid_credentials"
  }
}
```

HTTP 400 indicates an invalid health challenge, 401 indicates missing or invalid
authentication, and 403 indicates a session or origin permission error.
Existing layout-operation responses retain their original representation;
the browser displays both string errors and `error.message` responses.

The session error codes are registered here:

- `invalid_credentials`: missing or invalid authentication (401).
- `invalid_host`: unsupported local host (403).
- `invalid_origin`: Origin differs from the editor address (403).
- `cross_site_request`: browser identifies a cross-site request (403).
- `origin_required`: session mutation is missing its Origin header (403).
- `invalid_health_challenge`: health challenge has an invalid format (400).
- `read_only`: the selected session uses read-only mode (403).
