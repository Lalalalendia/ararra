/* One authenticated network boundary for the ReadEra browser. The raw team
 * key is exchanged once for a Secure HttpOnly cookie, never retained in JS.
 */
export function createSessionClient() {
  let csrfToken='';
  async function fetchSession(path, options={}) {
    return fetch(path,{credentials:'same-origin',cache:'no-store',...options});
  }
  async function receive(response) {
    if (response.status===401) throw new Error('Ключ недействителен или доступ отозван.');
    if (!response.ok) throw new Error('Сервер отказал в авторизации ('+response.status+').');
    const session=await response.json();
    if (!session?.csrf_token || !session?.member_id || !session?.role) {
      throw new Error('Некорректный ответ сервера авторизации.');
    }
    csrfToken=session.csrf_token;
    return session;
  }
  async function login(apiKey) {
    return receive(await fetchSession('/v1/session',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({api_key:apiKey}),
    }));
  }
  async function loginPassword(memberId,password) {
    return receive(await fetchSession('/v1/session/password',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({member_id:memberId,password}),
    }));
  }
  async function changePassword(currentPassword,newPassword) {
    if(!csrfToken)throw new Error('Войдите с паролем, прежде чем менять его.');
    const response=await fetchSession('/v1/session/password/change',{
      method:'POST',headers:{
        'Content-Type':'application/json','X-ReadEra-CSRF':csrfToken,
      },
      body:JSON.stringify({current_password:currentPassword,new_password:newPassword}),
    });
    if(!response.ok){
      let detail='';
      try {
        const data=await response.json();
        if(typeof data.detail==='string')detail=data.detail.slice(0,180);
      }catch{ /* Secrets and untrusted response bodies are never logged. */ }
      throw new Error(detail||'Смена пароля отклонена ('+response.status+').');
    }
    csrfToken='';
  }
  async function restore() {
    const response=await fetchSession('/v1/session');
    if(response.status===401){csrfToken='';return null;}
    return receive(response);
  }
  async function logout() {
    const response=await fetchSession('/v1/session/logout',{
      method:'POST',headers:{'X-ReadEra-CSRF':csrfToken},
    });
    if (!response.ok) throw new Error('Сервер не подтвердил выход ('+response.status+').');
    csrfToken='';
  }
  function clear() {csrfToken='';}
  async function request(path,{method='GET',body,headers={},responseType='json',...rest}={}) {
    if (!csrfToken) throw new Error('Сначала выполните вход в команду.');
    const mutating=!['GET','HEAD','OPTIONS'].includes(method.toUpperCase());
    const response=await fetchSession(path,{
      ...rest,method,body,
      headers:{...(mutating?{'X-ReadEra-CSRF':csrfToken}:{}),...headers},
    });
    if (!response.ok) {
      let detail='';
      try {
        const json=await response.json();
        detail=typeof json.detail==='string'?String(json.detail).slice(0,280):'';
      } catch { /* Do not surface untrusted HTTP response bodies. */ }
      if (response.status===401) throw new Error('Сессия истекла или участник отключён.');
      if (response.status===403) throw new Error('Недостаточно прав или неверный CSRF-токен.');
      if (response.status===404) throw new Error('Запись не найдена или недоступна.');
      if (response.status===409) throw new Error('Конфликт версий или назначений: '+detail);
      if (response.status===410) throw new Error('Срок хранения загрузки истёк.');
      if (response.status===413) throw new Error('Превышен разрешённый размер или квота загрузок.');
      throw new Error('Ошибка '+response.status+': '+(detail||'запрос отклонён'));
    }
    return responseType==='raw'?response:response.json();
  }
  return {login,loginPassword,changePassword,restore,logout,clear,request};
}
