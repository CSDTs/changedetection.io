# build and tag a local CSDT image, stand up changedetection with browser step clickable option (e.g. Playwright)
docker build -t changedetection.io . && docker rm playwright-chrome || true && docker rm changedetection || true && docker-compose up
