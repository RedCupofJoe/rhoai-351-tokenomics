FROM registry.access.redhat.com/ubi9/python-312
WORKDIR /opt/app-root/src
COPY --chown=1001:0 exporter/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=1001:0 tokenomics ./tokenomics
COPY --chown=1001:0 exporter ./exporter
ENV PYTHONPATH=/opt/app-root/src \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "exporter.main"]
