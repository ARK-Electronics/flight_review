"""Authenticated machine endpoints for private support-log analysis."""
# pylint: disable=relative-beyond-top-level,abstract-method
import tornado.web
from .api_key import authenticate_request_api_key
from .upload import UploadHandler
from .ai_analysis import AIAnalysisAPIHandler
from .analysis_jobs import AnalysisJobHandler


class APIKeyOnly(tornado.web.RequestHandler):
    """Require an approved account's header key; cookies/query keys are not used."""
    def get_current_user(self):
        """Resolve the account from a header, never a browser cookie."""
        if not (self.request.headers.get('Authorization', '').lower().startswith('bearer ')
                or self.request.headers.get('X-API-Key')):
            return None
        return authenticate_request_api_key(self)

    def check_xsrf_cookie(self):
        """Header credentials are not ambient browser credentials."""
        if not self.current_user:
            raise tornado.web.HTTPError(401)

    def prepare(self):
        """Reject before accepting an upload body or running analysis."""
        if not self.current_user:
            raise tornado.web.HTTPError(401)
        self.set_header('Cache-Control', 'no-store')
        return super().prepare()


@tornado.web.stream_request_body
class SupportUploadHandler(APIKeyOnly, UploadHandler):
    """Reuse bounded parsing while forcing private, notification-free uploads."""
    support_upload = True


class SupportAnalysisHandler(APIKeyOnly, AIAnalysisAPIHandler):
    """Submit or fetch the existing analysis with account/log authorization."""


class SupportJobHandler(APIKeyOnly, AnalysisJobHandler):
    """Poll the submitting account's durable job."""
