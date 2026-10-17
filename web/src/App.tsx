import { useEffect } from "react";

import Scene from "./scene/Scene";
import Overlay from "./ui/Overlay";
import { installVoice } from "./voice/flow";
import { installSpeech } from "./voice/speech";
import { connect } from "./ws";

export default function App() {
  useEffect(() => connect(), []);
  useEffect(() => {
    const stopSpeech = installSpeech();
    const stopVoice = installVoice();
    return () => {
      stopVoice();
      stopSpeech();
    };
  }, []);

  return (
    <>
      <div className="scene-layer">
        <Scene />
      </div>
      <Overlay />
    </>
  );
}
