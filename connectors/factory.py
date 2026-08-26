"""Connector construction. One transport: the local JCo REST bridge."""

import logging

from .jco_connector import JCoConnector

logger = logging.getLogger("basis_copilot.factory")


class ConnectorFactory:
    """Builds the SAP connector from settings."""

    @staticmethod
    def create(settings) -> JCoConnector:
        logger.info("SAP target %s sysnr %s client %s via bridge %s",
                    settings.sap_ashost, settings.sap_sysnr,
                    settings.sap_client, settings.jco_base_url)
        return JCoConnector(
            jco_url=settings.jco_base_url,
            ashost=settings.sap_ashost,
            sysnr=settings.sap_sysnr,
            client=settings.sap_client,
            user=settings.sap_user,
            passwd=settings.sap_passwd,
            lang=settings.sap_lang,
            timeout=settings.rfc_timeout_seconds,
        )
