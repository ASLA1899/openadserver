# Responsive Ad Tag

This note documents the responsive leaderboard support added for OpenAdServer.

## Website Tag

Use one script tag for the placement:

```html
<script src="https://media.aslalabs.org/api/v1/ad/tag.js?slot=homepage-leaderboard" async></script>
```

The tag makes one ad request per page load and chooses the size client-side:

| Viewport | Requested creative size |
|----------|--------------------------|
| `768px` and wider | `728x90` |
| `767px` and narrower | `300x250` |

Do not put separate desktop and mobile ad iframes on the page and hide one with
CSS. Browsers can still load both, which can count two impressions.

The `slot` value remains the logical placement ID for reporting. The selected
size is sent separately to the server as `size=728x90` or `size=300x250`.

Optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `desktop_size` | `728x90` | Creative size above the mobile breakpoint |
| `mobile_size` | `300x250` | Creative size at or below the mobile breakpoint |
| `breakpoint` | `767` | Mobile max width in CSS pixels |

## Server Behavior

The responsive support is additive:

- `GET /api/v1/ad/tag.js` generates the publisher JavaScript tag.
- `GET /api/v1/ad/embed` accepts optional `size=WIDTHxHEIGHT`.
- `GET /api/v1/ad/serve` accepts optional `size=WIDTHxHEIGHT`.
- Existing tags that encode dimensions in `slot`, such as
  `leaderboard-728x90`, continue to work.
- No database schema change is required.

## Production Details

- Host: `gregcohen@20.185.219.8`
- SSH port: `2222`
- App path: `/opt/openadserver`
- Runtime compose file: `/opt/openadserver/docker-compose.custom.yml`
- Services: `adserver` and `redis`
- Public hostnames: `https://media.aslalabs.org` and `https://ads.aslalabs.org`
- Internal ad server port on the host: `8421`

The production compose file should keep `restart: unless-stopped` on both
`adserver` and `redis`.

## Verification

Run these on the production host:

```bash
curl http://localhost:8421/health
curl 'http://localhost:8421/api/v1/ad/tag.js?slot=homepage-leaderboard'
curl 'http://localhost:8421/api/v1/ad/embed?slot=homepage-leaderboard&size=300x250'
curl 'http://localhost:8421/api/v1/ad/embed?slot=leaderboard-728x90'
```

Public checks:

```bash
curl 'https://media.aslalabs.org/api/v1/ad/tag.js?slot=homepage-leaderboard'
curl 'https://media.aslalabs.org/api/v1/ad/embed?slot=homepage-leaderboard&size=300x250'
```

## Rollback

Rollback artifacts from the initial responsive tag deployment:

- DB dump: `/opt/openadserver/backups/openadserver-pre-responsive-20260426T201302Z.sql.gz`
- Source snapshot: `/opt/openadserver/backups/openadserver-source-pre-responsive-20260426T201302Z.tar.gz`
- Docker image tag: `openadserver-adserver:rollback-pre-responsive-20260426T201302Z`

Rollback command sequence:

```bash
cd /opt/openadserver
tar -xzf /opt/openadserver/backups/openadserver-source-pre-responsive-20260426T201302Z.tar.gz -C /opt/openadserver
docker tag openadserver-adserver:rollback-pre-responsive-20260426T201302Z openadserver-adserver:latest
docker compose -f docker-compose.custom.yml up -d adserver redis
```

Use the DB dump only if a future deployment changes database data or schema.
The responsive tag deployment itself does not require a DB restore for rollback.
