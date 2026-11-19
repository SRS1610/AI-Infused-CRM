# Tessera web client: `dev` target runs Vite with HMR; `prod` serves the static build with nginx.
FROM node:22-alpine AS dev
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
EXPOSE 5173
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]

FROM dev AS build
RUN npm run build

FROM nginx:1.27-alpine AS prod
COPY deploy/images/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 80
