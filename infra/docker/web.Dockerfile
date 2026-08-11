FROM node:22-alpine

ARG CODEX_CLI_VERSION=0.147.0

RUN apk add --no-cache curl file jq poppler-utils python3 unzip zip \
  && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
  && codex --version

WORKDIR /app

COPY package*.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm install

COPY . .
WORKDIR /app/apps/web
RUN touch .env.local && npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]
