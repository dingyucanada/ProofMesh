FROM agentteams/qwenpaw-worker:v1.2.0-beta.1

LABEL org.opencontainers.image.title="ProofMesh AgentTeams QwenPaw beta compatibility layer" \
      org.opencontainers.image.version="v1.2.0-beta.1-compat2" \
      org.opencontainers.image.revision="78d0ceda336befa6e62bf89fc1a6b08b965e128d" \
      proofmesh.io/compatibility-fix="qwenpaw-plugin-path-and-acp-api"

# v1.2.0-beta.1 installs the built-in plugins under /opt/hiclaw, while the
# qwenpaw-worker default resolves /opt/agentteams. The beta Worker REST API
# also drops spec.env, so a manifest-only override cannot reach the container.
# Its unconstrained ACP dependency currently resolves past the API consumed by
# QwenPaw; 0.10.1 is the latest release tested to expose that beta API.
RUN mkdir -p /opt/agentteams \
    && ln -s /opt/hiclaw/qwenpaw-builtin /opt/agentteams/qwenpaw-builtin \
    && /opt/venv/qwenpaw/bin/pip install --no-cache-dir "agent-client-protocol==0.10.1"
