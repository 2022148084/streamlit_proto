import streamlit as st
import json
from openai import OpenAI
import requests  # Google API 호출
import folium  # ⭐️ 지도 UI를 위한 folium
from streamlit_folium import st_folium  # ⭐️ Streamlit에 folium을 띄우기 위함

# --- 1. OpenAI 및 Google Maps API 클라이언트 설정 ---
try:
    openai_client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
    google_maps_api_key = st.secrets["GOOGLE_MAPS_API_KEY"]
except Exception as e:
    st.error("API 키가 설정되지 않았습니다. .streamlit/secrets.toml 파일에 'OPENAI_API_KEY'와 'GOOGLE_MAPS_API_KEY'가 모두 있는지 확인하세요.")
    st.stop()

# --- 2. 챗 파서를 위한 시스템 프롬프트 ---
CHAT_PARSER_PROMPT = """
너는 카카오톡 대화 내용을 분석하여 핵심 키워드를 추출하는 전문가야.
사용자의 대화 내용에서 [약속 장소], [음식/메뉴], [시간], [주요 제약 조건]과 관련된 핵심 단어만 뽑아내.
[규칙]
1. 날짜, 시간, 사람 이름은 **무시해.**
2. "사진", "이모티콘", "샵검색", "파일" 같은 시스템 메시지는 **무시해.**
3. 인사말("안녕", "잘가"), 잡담("ㅋㅋㅋ", "ㅠㅠ")은 **무시해.**
4. "거기 차 댈 데 있어?" -> "주차" 처럼, **의미를 요약**해서 키워드로 만들어.
5. 오직 **JSON 객체(Dictionary) 형식**으로만 응답해.
6. JSON 객체는 **"keywords"**라는 키를 가져야 하고, 그 값은 **키워드 문자열의 리스트**여야 해.
[예시]
{"keywords": ["강남역", "파스타", "조용한 곳", "카페", "보드게임카페", "영화관", "쇼핑"]}
"""

# --- 3. 플랜(검색어) 생성기를 위한 프롬프트 ---
QUERY_GENERATOR_PROMPT = """
너는 사용자의 키워드를 바탕으로, Google 지도 검색에 사용할 검색어 3개를 생성하는 AI야.

[규칙]
1. [사용자 키워드]와 [추가 요청사항]을 조합해서, '식당', '카페', '문화/활동' 순서로 이어지는 1개의 플랜을 만들어.
2. 각 장소는 Google 지도 검색에 최적화된 "장소 + 키워드" 형태의 검색어여야 해.
   (예: "강남역 파스타", "강남역 분위기 좋은 카페", "강남역 CGV")
3. "근처", "주변", "가까운" 단어는 제외.
4. 오직 **JSON 객체(Dictionary) 형식**으로만 응답해.
5. JSON 객체는 **"plan"**이라는 키를 가져야 하고, 그 값은 3개의 **검색어 문자열 리스트**여야 해.
6. **문화** 라는 단어가 들어간 거 절대 넣지마 문화센터 등.

[예시]
{"plan": ["강남역 파스타", "강남역 분위기 좋은 카페", "강남역 CGV"]}
"""

# --- 4. 세션 상태 초기화 ---
if 'page' not in st.session_state:
    st.session_state.page = 'upload'
if 'keywords' not in st.session_state:
    st.session_state.keywords = []
if 'plan' not in st.session_state:
    st.session_state.plan = []
if 'deleted_places_set' not in st.session_state:
    st.session_state.deleted_places_set = set() 
if 'user_regenerate_prompt' not in st.session_state:
    st.session_state.user_regenerate_prompt = "" 
# ⭐️ [신규] 1단계 AI가 생성한 검색어를 저장할 곳 (디버깅용)
if 'generated_queries' not in st.session_state:
    st.session_state.generated_queries = []

# --- 5. 헬퍼 함수 (페이지 전환 및 로직) ---

def go_to_refine():
    """ (Screen 1 -> 1.5) 챗 파서 API 호출 """
    if st.session_state.kakao_file is not None:
        with st.spinner("대화 내용을 분석 중입니다... 🤖"):
            try:
                uploaded_file = st.session_state.kakao_file
                chat_content = uploaded_file.getvalue().decode("utf-8")
                
                response = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": CHAT_PARSER_PROMPT},
                        {"role": "user", "content": chat_content}
                    ],
                    response_format={"type": "json_object"}
                )
                
                response_text = response.choices[0].message.content
                data = json.loads(response_text)
                
                if isinstance(data, dict) and 'keywords' in data and isinstance(data['keywords'], list):
                    st.session_state.keywords = data['keywords']
                else:
                    st.error("AI가 예상치 못한 형식으로 키워드를 반환했습니다.")
                    st.session_state.keywords = []

                st.session_state.page = 'refine'
            
            except json.JSONDecodeError:
                st.error("AI가 키워드 리스트를 만드는 데 실패했습니다. (JSON 변환 오류)")
            except Exception as e:
                st.error(f"채팅 분석 중 오류가 발생했습니다: {e}")
                
    else:
        st.toast("파일을 먼저 업로드해줘!", icon="⚠️")

def go_to_result():
    """
    (Screen 1.5 -> 2)
    1. AI로 검색어 3개 생성 -> 2. Google Maps API 3번 호출
    """
    st.session_state.deleted_places_set = set()
    st.session_state.user_regenerate_prompt = ""
    
    user_prompt = st.session_state.user_prompt_input
    active_keywords = st.session_state.selected_keywords
    
    try:
        # --- 1단계: AI 호출 (검색어 3개 생성) ---
        combined_prompt = f"""
        [사용자 키워드]
        {', '.join(active_keywords)}

        [추가 요청사항]
        {user_prompt}
        """
        
        with st.spinner("AI가 검색할 키워드 3개를 생성 중입니다... (1/2)"):
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": QUERY_GENERATOR_PROMPT},
                    {"role": "user", "content": combined_prompt}
                ],
                response_format={"type": "json_object"}
            )
            response_text = response.choices[0].message.content
            query_json = json.loads(response_text)
            search_queries = query_json.get("plan", []) 
            
            # ⭐️ [신규] 생성된 검색어를 세션에 저장 (디버깅용)
            st.session_state.generated_queries = search_queries

            if not search_queries:
                st.error("AI가 검색어를 생성하지 못했습니다.")
                return

        # --- 2단계: Google Maps API 호출 (3번) ---
        google_api_results = []
        with st.spinner("Google Maps에서 '진짜' 장소를 검색 중입니다... (2/2)"):
            search_url = "https://places.googleapis.com/v1/places:searchText"
            field_mask = "places.displayName,places.location,places.googleMapsUri"

            for query in search_queries:
                payload = {"textQuery": query, "languageCode": "ko"}
                headers = {
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": google_maps_api_key,
                    "X-Goog-FieldMask": field_mask
                }
                
                response = requests.post(search_url, json=payload, headers=headers)
                response.raise_for_status() 
                
                result_data = response.json()
                
                if result_data.get("places"):
                    top_place = result_data["places"][0]
                    google_api_results.append(top_place)
                else:
                    google_api_results.append({"error": "No results found", "query": query})

        # --- 3단계: 최종 저장 ---
        st.session_state.plan = google_api_results 
        st.session_state.page = 'result'

    except requests.exceptions.RequestException as e:
        st.error(f"Google Maps API 호출 오류: {e}")
        st.write("API 응답:", response.text) 
    except json.JSONDecodeError as e:
        st.error(f"AI 응답 처리 중 JSON 오류가 발생했습니다: {e}")
    except Exception as e:
        st.error(f"알 수 없는 오류가 발생했습니다: {e}")

# ⭐️ [수정됨] Screen 2의 삭제/복구 버튼을 위한 헬퍼 함수
def toggle_delete_place(place_name):
    """
    'st.checkbox'의 'on_change' 시 호출되어,
    1. 'deleted_places_set'의 상태를 토글하고
    2. 'user_regenerate_prompt' 텍스트를 자동 업데이트함
    """
    
    # 1. '삭제' 누른 장소 Set을 토글 (추가 또는 제거)
    if place_name in st.session_state.deleted_places_set:
        st.session_state.deleted_places_set.remove(place_name) # (복구)
    else:
        st.session_state.deleted_places_set.add(place_name) # (삭제)

    # 2. '추가 요청사항' 텍스트 박스의 값을 '읽어옴'
    current_prompt = st.session_state.user_regenerate_prompt
    
    # 3. 텍스트에서 "OO 제외"가 아닌, '순수' 사용자 입력만 걸러냄
    parts = current_prompt.split(', ')
    pure_parts = [p for p in parts if not p.endswith(" 제외") and p.strip()]
    pure_prompt = ", ".join(pure_parts)

    # 4. '삭제'된 장소 목록으로 "OO 제외" 텍스트를 '새로 만듦'
    deleted_parts = [f"{name} 제외" for name in st.session_state.deleted_places_set]

    # 5. '순수' 입력과 '삭제' 텍스트를 '조합'
    if pure_prompt and deleted_parts:
        st.session_state.user_regenerate_prompt = f"{pure_prompt}, {', '.join(deleted_parts)}"
    elif pure_prompt:
        st.session_state.user_regenerate_prompt = pure_prompt
    elif deleted_parts:
        st.session_state.user_regenerate_prompt = ", ".join(deleted_parts)
    else:
        st.session_state.user_regenerate_prompt = ""

# --- 6. 메인 로직 (페이지 라우터) ---

# ----------------------------------------------
# 화면 1: 파일 업로드 (screen1.html)
# ----------------------------------------------
if st.session_state.page == 'upload':
    st.title("카카오톡 채팅 기반 계획 생성기")
    
    st.file_uploader(
        "카카오톡 대화 내용(.txt)을 업로드하세요", 
        type=['txt'], 
        key='kakao_file' 
    )
    
    st.button("키워드 추출하기", on_click=go_to_refine)

# ----------------------------------------------
# 화면 1.5: 키워드 확인 및 수정 (screen1_5_refine.html)
# ----------------------------------------------
elif st.session_state.page == 'refine':
    st.title("대화에서 키워드를 찾았어요")
    
    st.multiselect(
        label="플랜에 반영할 키워드를 확인/삭제하세요.", 
        options=st.session_state.keywords,    
        default=st.session_state.keywords,    
        key='selected_keywords'               
    )

    if not st.session_state.keywords:
         st.info("추출된 키워드가 없네요. 추가 요청사항을 직접 입력해 주세요.")
    
    st.text_input("추가 요청사항을 입력하세요", 
                  placeholder="예: 주차 가능한 곳, 도보 10분 이내", 
                  key='user_prompt_input')
    
    st.button("이 조건으로 플랜 생성하기", on_click=go_to_result)

# ----------------------------------------------
# ⭐️⭐️⭐️ [수정됨] 화면 2: 플랜 제안 (디버깅 UI 추가) ⭐️⭐️⭐️
# ----------------------------------------------
elif st.session_state.page == 'result':
    st.title("AI 추천 플랜 (1개)")
    
    # ⭐️ [신규] AI가 생성한 '검색어'를 펼쳐보기로 보여줌 (디버깅용)
    with st.expander("🤖 AI가 생성한 검색어 (1단계 결과)"):
        if st.session_state.generated_queries:
            st.write(st.session_state.generated_queries)
        else:
            st.write("생성된 검색어가 없습니다.")
    
    if st.session_state.plan and len(st.session_state.plan) > 0:
        
        col1, col2 = st.columns([0.6, 0.4]) # 지도 60%, 리스트 40%
        
        # --- 1-1. 왼쪽 (지도) ---
        with col1:
            st.subheader("📍 플랜 지도")
            
            try:
                # '삭제'되지 않은 첫 번째 장소를 지도의 중심으로 사용
                center_lat, center_lon = None, None
                for place in st.session_state.plan:
                    if 'location' in place and place['displayName']['text'] not in st.session_state.deleted_places_set:
                        center_lat = place['location']['latitude']
                        center_lon = place['location']['longitude']
                        break
                
                # (만약 다 삭제됐으면) 그냥 첫 번째 장소를 중심으로 씀
                if center_lat is None and st.session_state.plan[0].get('location'):
                    first_location = st.session_state.plan[0]['location']
                    center_lat = first_location['latitude']
                    center_lon = first_location['longitude']
                elif center_lat is None: # 모든 장소에 location이 없을 최악의 경우
                    center_lat = 37.4979 # (강남역)
                    center_lon = 127.0276

                m = folium.Map(location=[center_lat, center_lon], zoom_start=15)
                
                for i, place in enumerate(st.session_state.plan):
                    if 'location' in place:
                        lat = place['location']['latitude']
                        lon = place['location']['longitude']
                        name = place['displayName']['text']
                        is_deleted = name in st.session_state.deleted_places_set
                        
                        folium.Marker(
                            location=[lat, lon],
                            popup=f"<b>{i+1}. {name}</b>",
                            tooltip=name,
                            icon=folium.Icon(color='gray' if is_deleted else 'blue', icon='info-sign')
                        ).add_to(m)
                
                st_folium(m, use_container_width=True, height=400)

            except Exception as e:
                st.error(f"지도 생성 중 오류가 발생했습니다: {e}")
                st.write(st.session_state.plan) 

        # --- 1-2. 오른쪽 (장소 리스트) ---
        with col2:
            st.subheader("📝 장소 목록")
            
            for i, place in enumerate(st.session_state.plan):
                with st.container(border=True):
                    if 'displayName' in place:
                        name = place['displayName']['text']
                        url = place['googleMapsUri']
                        
                        is_deleted = name in st.session_state.deleted_places_set
                        is_kept = not is_deleted

                        st.checkbox(
                            f"**{i+1}. {name}**",
                            value=is_kept,
                            key=f"toggle_{name}",
                            on_change=toggle_delete_place, 
                            args=(name,)
                        )
                        
                        st.link_button("🔗 Google 지도로 보기", url, use_container_width=True, disabled=(not is_kept))
                    
                    elif 'error' in place:
                        st.error(f"장소 {i+1}을(를) 찾지 못했습니다. (검색어: {place.get('query')})")
        
    else:
        st.error("플랜이 생성되지 않았습니다. 뒤로 돌아가 다시 시도해 주세요.")
    
    # ----------------------------------------------------
    # ⭐️ 하단 플랜 수정 (재생성) UI (이전과 동일)
    # ----------------------------------------------------
    st.divider()
    st.subheader("🔁 플랜 수정하기")
    
    st.text_input("추가 요청사항", 
                 key='user_regenerate_prompt', 
                 placeholder="예: 도보 10분 이내 (장소 삭제 시 자동 반영)")
    
    if st.button("이 조건으로 다시 생성하기"):
        st.toast("아직 구현 안 함 ㅋㅋㅎㅎㅈㅅ", icon="🤪")
    
    if st.button("◀ 키워드 수정으로 돌아가기"):
        st.session_state.page = 'refine'
        st.rerun()