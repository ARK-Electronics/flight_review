# Encrypted logs
To be able to handle encrypted logs you need to add/replace the dummy key with your private key in the file
  ```bash
   flight_review/app/private_key/private_key.pem
   ```

Once you have the private key there, you should be able to upload encrypted logs and display them using the corresponding private key.
## Support integration API

An approved non-admin service account can use its API key in the Authorization: Bearer header. Cookie-only and query-key authentication are rejected on these routes.

- POST /api/support/upload: multipart filearg (.ulg, .bin, .csv), description and email fields. Returns {"url":"/plot_app?log=<uuid>"}. Uploads are forced private, excluded from statistical sharing, and send no upload notification emails. Retries deduplicate within the uploading account and support source.
- GET /api/support/analysis?log=<uuid>: returns cached analysis or {"cached":false}.
- POST /api/support/analysis?log=<uuid>: JSON {"effort":"medium"}; returns HTTP 202 with job_id and state. Uses the existing isolated parsing/AI worker and account quotas.
- GET /api/support/jobs/<job_id>: returns state and result (analysis, data_summary, model) when done. Poll at least 20 seconds apart; 429/503 should back off. Jobs/results require the same still-approved submitting account.

Support imports are inaccessible on legacy plot/download routes except to their uploader or approved administrators. The support app must separately enforce ticket ownership on its own log downloads. Never put API keys into URLs, public code, or browser requests.
