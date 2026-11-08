"""Korean market data provider (DATA_PROVIDER=kr).

    symbols.py       종목코드 / 종목명 / DART corp_code 변환 (파일 캐시)
    dart.py          OpenDART REST 클라이언트 (호출 간격, 재시도, 디스크 캐시)
    prices.py        일별 시세: pykrx(KRX 로그인) → FinanceDataReader → FDR GitHub 스냅샷
    accounts.yaml    계정과목 매핑표 (account_id 우선, account_nm 후보)
    accounts.py      매핑표로 DART 응답에서 항목 추출
    fundamentals.py  분기 차감, TTM, 재무지표 계산 (순수 함수, 오프라인 테스트 대상)
    client.py        KRDataClient — 기존 DataClient 프로토콜을 그대로 구현

Nothing here is imported in US mode.
"""
