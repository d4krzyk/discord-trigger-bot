FROM openjdk:17-jdk

WORKDIR /app
COPY Lavalink.jar application.yml ./

EXPOSE 2333

CMD ["java", "-jar", "Lavalink.jar"]
