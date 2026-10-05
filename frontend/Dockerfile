FROM node:24-slim as build-stage
WORKDIR /app
COPY package*.json ./
# 使用 npm ci 配合 package-lock.json 确保构建一致性
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
RUN npm ci
COPY . .
RUN npm run build

# Production stage
FROM nginx:stable-alpine as production-stage
COPY --from=build-stage /app/dist /usr/share/nginx/html
ENV RUNTIME_UI_CSP_SOURCE=http://*.skycloud.localhost:8081
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
