# Everything the published site cannot do needs a checkout and a process.
# This is both, in one command:
#
#   docker build -t prophecy . && docker run -p 8000:8000 prophecy
#
# Then open http://127.0.0.1:8000. To point it at your own repository
# instead of the demo:
#
#   docker run -p 8000:8000 -v /path/to/repo:/repo -e PROPHECY_REPO=/repo prophecy

FROM python:3.12-slim

# git is not a convenience here: every answer this gives comes from shelling
# out to it
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e .

RUN git config --global user.email "demo@prophecy.local" \
 && git config --global user.name "Prophecy demo" \
 && git config --global init.defaultBranch main \
 && git config --global --add safe.directory '*'

# Built at image time so the container starts instantly, with five branches
# in flight and agents already at work.
RUN prophecy demo /demo

ENV PROPHECY_REPO=/demo
EXPOSE 8000
CMD ["sh", "-c", "prophecy -C ${PROPHECY_REPO} serve --port 8000 --open"]
