FROM node:22-alpine

WORKDIR /app

COPY package*.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm install

COPY . .
WORKDIR /app/apps/web
RUN touch .env.local && npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]
