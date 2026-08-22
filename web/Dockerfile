FROM node:22-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM dependencies AS build
ARG NEXT_PUBLIC_API_ORIGIN=/api
ARG NEXT_PUBLIC_ENABLE_DEV_LOGIN=true
ENV NEXT_PUBLIC_API_ORIGIN=$NEXT_PUBLIC_API_ORIGIN
ENV NEXT_PUBLIC_ENABLE_DEV_LOGIN=$NEXT_PUBLIC_ENABLE_DEV_LOGIN
COPY . .
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3001
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/dist ./dist
COPY --from=dependencies /app/node_modules/react ./dist/standalone/node_modules/react
COPY --from=dependencies /app/node_modules/react-dom ./dist/standalone/node_modules/react-dom
COPY --from=dependencies /app/node_modules/scheduler ./dist/standalone/node_modules/scheduler
EXPOSE 3001
CMD ["node", "dist/standalone/server.js"]

FROM nginx:1.27-alpine AS gateway
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/client /usr/share/nginx/html
EXPOSE 80
