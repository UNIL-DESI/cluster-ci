"""HTTP transport for configured cluster endpoints, never redirected elsewhere."""
import urllib.request


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cluster_urlopen(request, timeout):
    # urllib otherwise forwards Authorization on cross-origin GET redirects.
    # Refuse all redirects, including those that might resend upload bodies.
    return urllib.request.build_opener(_NoRedirects()).open(request, timeout=timeout)
