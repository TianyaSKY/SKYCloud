FROM node:24-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 as build-stage
WORKDIR /app
COPY package*.json ./
# 使用 npm ci 配合 package-lock.json 确保构建一致性
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
RUN npm ci
COPY . .
RUN npm run build

# Production stage
FROM nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94 as production-stage
COPY --from=build-stage /app/dist /usr/share/nginx/html
ENV RUNTIME_UI_CSP_SOURCE=http://*.skycloud.localhost:8081
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
