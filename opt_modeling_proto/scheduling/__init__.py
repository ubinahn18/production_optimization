# -*- coding: utf-8 -*-
"""scheduling 패키지: 30일 생산 스케줄링 최적화 모델을 기능별로 나눈 모듈들.

- models: 시간 구조 상수 + 입력/설정/결과 데이터클래스
- solver: CP-SAT 모델 구성 및 2단계(비용 -> 연속성) 풀이
- report: 콘솔 리포트 출력, CSV 저장, 간트 차트 PNG 생성
- example_data: 내장 예시 데이터 및 JSON 데이터 로더

실행 진입점은 opt_modeling_proto/schedule_optimizer.py (이 패키지의 상위
폴더)에 있다: `python schedule_optimizer.py`로 실행한다.
"""
