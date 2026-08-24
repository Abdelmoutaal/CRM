FROM twentycrm/twenty:v2.32.0
COPY customizations/index.js /app/packages/twenty-emails/dist/index.js
COPY customizations/logo.png /app/packages/twenty-server/dist/front/images/icons/android/android-launchericon-192-192.png