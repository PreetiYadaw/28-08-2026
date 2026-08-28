
from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    APP_NAME: str = "Testcase Generation App"
    ADMIN_EMAIL: str = ""
    API_VERSION: str = "v1"
    API_URL_PREFIX: str = "testcase-generation"
    
    YONO_2_NAME: str = "YONO 2.0"
    YONO_2_DESCRIPTION: str = "Comprehensive mobile-first digital banking platform integrating banking, loans, investments, cards, insurance, and lifestyle services. Built on microservices architecture and connected to CBS, IPH, and UPI for real-time transaction processing."

    TRADE_FINANCE_NAME: str = "Trade Finance"
    TRADE_FINANCE_DESCRIPTION: str = "Specialized banking system supporting domestic and international trade including Letters of Credit, Bank Guarantees, export-import financing, and Supply Chain Finance. Integrated with CBS, SWIFT, compliance, and risk systems."

    INB_NAME: str = "INB"
    INB_DESCRIPTION: str = "Internet Banking platform enabling online fund transfers, bill payments, tax payments, and account services. Connected to IPH for NEFT, RTGS, and IMPS with strong authentication mechanisms."

    UPI_NAME: str = "UPI"
    UPI_DESCRIPTION: str = "Real-time payment system supporting instant peer-to-peer and merchant transactions, QR payments, and VPA-based transfers. Integrated with NPCI switch and backend banking systems for 24x7 settlement."

    YONO_BUSINESS_NAME: str = "YONO Business"
    YONO_BUSINESS_DESCRIPTION: str = "Corporate and SME digital banking platform supporting bulk payments, salary uploads, tax payments, and trade services with role-based access and maker-checker workflows."

    ATM_NAME: str = "ATM"
    ATM_DESCRIPTION: str = "Automated Teller Machine channel providing cash withdrawal, balance enquiry, and mini statements using ISO 8583 messaging and real-time CBS connectivity."

    IPH_NAME: str = "IPH"
    IPH_DESCRIPTION: str = "Integrated Payment Hub acting as centralized payment processor for IMPS, NEFT, and RTGS. Routes transactions between digital channels and CBS with reconciliation and settlement management."

    GBSS_PLUS_NAME: str = "GBSS+"
    GBSS_PLUS_DESCRIPTION: str = "Global Business Support System Plus handling international banking operations, forex transactions, and cross-border remittances integrated with SWIFT and AML systems."

    CBS_NAME: str = "CBS"
    CBS_DESCRIPTION: str = "Core Banking Solution serving as the central backbone maintaining customer accounts, deposits, loans, and transaction ledgers with real-time updates across all banking channels."

    FIGS_PLUS_NAME: str = "FIGS+"
    FIGS_PLUS_DESCRIPTION: str = "Financial Inclusion and Government Schemes platform managing DBT, pensions, subsidies, and rural banking initiatives integrated with government systems and CBS."

    NBC_NAME: str = "NBC"
    NBC_DESCRIPTION: str = "New Business Channel enabling digital onboarding, eKYC, Aadhaar authentication, and video KYC integrated with regulatory verification and core banking systems."

    API_NAME: str = "API"
    API_DESCRIPTION: str = "Application Programming Interface layer enabling secure REST-based communication between internal banking systems and external partners with OAuth authentication and rate limiting."        
    
settings = Settings()
