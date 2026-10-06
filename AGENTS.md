# Agent Instructions

## Local mode and confidentiality

Ensure the local mode is always implemented and respected in new features. The only exceptions are 1) the console logs and other general metrics that are made available on the local network for the dashboard and 2) the files that are available on the local network but protected by the cluster token. In no case should a job submitted with the --local flag or its associated data be uploaded to a third-party server, including GitHub, or served publicly. The threat model to use for this is the following: everyone on the cluster (GitHub org access to the cluster token) is fully trusted (e.g., containers are considered a convenience and not a security feature, and container isolation features can be ignored), the local network is trusted for console outputs and job metadata, and public web access or third-party servers are completely off limits.
