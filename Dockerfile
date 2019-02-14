FROM scrapinghub/splash:3.2

ADD . /app
RUN pip3 install /app
