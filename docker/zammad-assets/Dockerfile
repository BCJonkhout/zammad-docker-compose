ARG BASE_IMAGE=ghcr.io/zammad/zammad:7.0.0-0042
FROM ${BASE_IMAGE}

USER root
COPY --chown=zammad:zammad prudai-support.css /opt/zammad/app/assets/stylesheets/custom/prudai-support.css
COPY --chown=zammad:zammad prudai-logo.png /opt/zammad/public/prudai-logo.png
COPY --chown=zammad:zammad prudai-logo.png /opt/zammad/public/assets/prudai-logo.png
COPY --chown=zammad:zammad application.html.erb /opt/zammad/app/views/layouts/application.html.erb
COPY prudai-entrypoint.sh /usr/local/bin/prudai-entrypoint
RUN chmod +x /usr/local/bin/prudai-entrypoint

ENTRYPOINT ["/usr/local/bin/prudai-entrypoint"]
