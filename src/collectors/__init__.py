"""Data collectors for official Norwegian registers and permitted web sources."""

from src.collectors.jobs import JobPostingCollector
from src.collectors.news import CompanyNewsCollector
from src.collectors.registry import RegistryCollector
from src.collectors.website import WebsiteCollector

__all__ = [
    "JobPostingCollector",
    "CompanyNewsCollector",
    "RegistryCollector",
    "WebsiteCollector",
]
