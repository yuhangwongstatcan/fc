# Certain environment variables are expected to be mounted at runtime from K8s secrets.
# - S3PROXY_ACCESS_KEY: changeme
# - S3PROXY_SECRET_KEY: changeme

# Base image for both stages. A standard venv does not bundle the Python
# interpreter (unlike conda-pack), so the runtime stage must use the same image
# as the build stage.
# Outside the StatCan network, tag the Docker Hub equivalent with this name first:
#   docker pull python:3.9.23-slim
#   docker tag python:3.9.23-slim artifactory.cloud.statcan.ca/docker/python:3.9.23-slim
FROM artifactory.cloud.statcan.ca/docker/python:3.9.23-slim AS base_image

FROM base_image AS builder
ARG APP_HOME=/home/jovyan
# Create the venv at the same path it will have in the final image, since a venv
# hard-codes its own location. CI activates it with
# `source /home/jovyan/src/venv/bin/activate`.
WORKDIR ${APP_HOME}/src
COPY src/requirements.txt .
# --no-deps: requirements.txt is a full lock (every transitive dependency is
# pinned). See the header of requirements.txt for why this is required.
RUN python -m venv venv \
    && venv/bin/pip install --no-cache-dir --no-deps -r requirements.txt

FROM base_image AS runtime

# Create non-root user for container
ARG USER=jovyan
ARG APP_HOME=/home/jovyan

RUN adduser --system --group $USER \
    && mkdir -p $APP_HOME \
    && chown -R $USER:$USER $APP_HOME

WORKDIR ${APP_HOME}
# Make public directory at the project root for the docs site
RUN mkdir public
COPY . ${APP_HOME}
# Copy the venv from the build stage
COPY --from=builder ${APP_HOME}/src/venv ${APP_HOME}/src/venv
WORKDIR ${APP_HOME}/src

# Use the venv by default. matplotlib renders to files only (no display in the
# container), so select the non-interactive backend.
ENV PATH=${APP_HOME}/src/venv/bin:$PATH \
    MPLBACKEND=Agg

# Create .env file and set non-sensitive defaults and create a directory for
# flask session objects to be saved in. Line endings are normalised in case the
# image is built from a Windows checkout.
RUN sed 's/\r$//' default.env > .env \
    && mkdir flask_session \
    && chown $USER:$USER flask_session

# Start container as non-root user
USER $USER

# ENV WEBAPP_BASE_URL=http://localhost:8000/

# If no command/parameters are passed, then default to running the webapp
# Otherwise, run the specified command/parameters when the container is started
CMD ["gunicorn", "main:app", "-b", "0.0.0.0:8000", "--timeout", "7200"]
# We only want to publish port 8000 in the case where the container from this image
# hosts the Frontier Counts webapp.
# Note that EXPOSE does not actually publish the port; to publish a port, the `-p`
# flag needs to be set at runtime.
EXPOSE 8000
