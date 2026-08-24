// Supabase 접속 설정
// -----------------------------------------------------
// 선물 매매일지 앱(trading-journal-cloud.html)에 들어있던 값과 동일합니다.
// 같은 프로젝트를 쓰되, 테이블은 pf_ 접두사로 완전히 분리돼 있습니다.
//
// anon key는 공개돼도 되는 키입니다 (RLS가 본인 데이터만 접근하도록 막아줍니다).
// service_role 키는 절대 여기 넣지 마세요 — GitHub Secrets 전용입니다.
// -----------------------------------------------------
const CONFIG = {
  SUPABASE_URL: "https://wngwnpuqagzkdifjfnnh.supabase.co",
  SUPABASE_ANON_KEY: "sb_publishable_u469Jki_xxsMU8-rToOePw_-O-9XIhY",
};
