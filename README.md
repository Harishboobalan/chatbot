# AI Core destination chat

A small Flask chatbot that calls SAP AI Core **only through a BTP destination**, so the app holds no AI Core credentials.

## How it works
1. Gets a token for the BTP Destination service (from its binding or service key).
2. Looks up your destination. For OAuth2ClientCredentials destinations, the Destination service returns a ready-to-use AI Core token.
3. Lists the RUNNING deployments at `GET {URL}/v2/lm/deployments`. If this works, the destination works.
4. Sends chat messages to the deployment you pick:
   - orchestration: `/v2/completion`
   - azure-openai: `/chat/completions`
   - aws-bedrock: `/invoke`

Errors show which step failed (`destination-service`, `destination-lookup`, `destination-auth`, `ai-core`, `llm`) along with the raw response.

## Run locally
1. Create a service key for your Destination service instance and save its JSON as `destination-key.json` in this folder.
2. Run:
   ```
   .venv\Scripts\activate
   set AICORE_DESTINATION=<your-destination-name>
   python app.py
   ```
3. Open http://localhost:5000, click **Test destination**, then chat.

## Deploy to Cloud Foundry
Edit `manifest.yml` (destination name + destination service instance), then run `cf push`.

## Destination expectations
- URL: the AI Core API URL, e.g. `https://api.ai.prod.<region>.aws.ml.hana.ondemand.com` (a trailing `/v2` is fine)
- Authentication: `OAuth2ClientCredentials`, with the token service URL `<uaa-url>/oauth/token` and the AI Core client id/secret
- Optional additional property: `URL.headers.AI-Resource-Group` = `<group>`
