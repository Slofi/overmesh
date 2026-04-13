FROM alpine:latest as compiler
ENV PYTHONUNBUFFERED 1

RUN apk update && apk add git python3 py3-pip

WORKDIR /
RUN git clone https://github.com/Slofi/overmesh.git && cd overmesh && python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -Ur /overmesh/requirements.txt


FROM alpine:latest as runner

RUN apk update && apk add git python3 py3-pip

WORKDIR /overmesh
COPY --from=compiler /opt/venv /opt/venv
COPY --from=compiler /overmesh /overmesh

RUN cp config.example.json config.json
ENV PATH="/opt/venv/bin:$PATH"
COPY . /overmesh/
RUN addgroup -S appgroup && adduser -S overmesh -G appgroup -D && chown overmesh /overmesh

USER overmesh
EXPOSE 8082
WORKDIR /overmesh

CMD [ "python3", "app.py" ]
