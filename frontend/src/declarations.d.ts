declare module '*.module.css' {
  const classes: { [key: string]: string };
  export default classes;
} 
declare module '*.css' {
  const content: any;
  export default content;
}

declare module '*.png';
declare module '*.jpg';
declare module '*.jpeg';
declare module '*.gif';
declare module '*.svg';

declare global {
  interface Window {
    __AUTH_URL__?: string;
  }
}

export {};
