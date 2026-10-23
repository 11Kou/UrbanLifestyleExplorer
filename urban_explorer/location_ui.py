"""Opt-in browser location button; no map service keys enter the browser."""
import streamlit as st


location_button = st.components.v2.component(
    "city_location",
    html='<button type="button">📍 定位当前城市</button><span role="status" aria-live="polite"></span>',
    css="""
    button { width:100%; padding:.55rem .7rem; border:1px solid #b9c9be;
      border-radius:8px; background:transparent; color:var(--st-text-color, #243a30);
      font:inherit; cursor:pointer; }
    button:disabled { opacity:.6; cursor:wait; }
    span { display:block; font-size:.8rem; margin-top:.3rem; }
    """,
    js="""
    export default function({ parentElement, setTriggerValue }) {
      const button = parentElement.querySelector('button');
      const status = parentElement.querySelector('span');
      let active = true;
      button.onclick = () => {
        if (!navigator.geolocation || !window.isSecureContext) {
          setTriggerValue('position', { error: '当前浏览器不支持定位，请使用 HTTPS、本机浏览器或手动输入城市。' });
          return;
        }
        button.disabled = true;
        status.textContent = '请允许浏览器访问位置，正在定位…';
        navigator.geolocation.getCurrentPosition(
          ({ coords }) => {
            if (!active) return;
            button.disabled = false;
            status.textContent = '';
            setTriggerValue('position', { longitude: coords.longitude, latitude: coords.latitude });
          },
          error => {
            if (!active) return;
            button.disabled = false;
            status.textContent = '';
            const messages = {
              1: '未获位置权限。可在浏览器设置中允许位置，或使用网络定位、手动输入城市。',
              2: '设备暂时无法获取位置，请使用网络定位或手动输入城市。',
              3: '获取位置超时，请重试或使用网络定位。'
            };
            setTriggerValue('position', { error: messages[error.code] || '定位失败，请手动输入城市。' });
          },
          { enableHighAccuracy: false, timeout: 12000, maximumAge: 60000 }
        );
      };
      return () => { active = false; button.onclick = null; };
    }
    """,
)
